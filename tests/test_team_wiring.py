"""全链路装配验证（不联网）：Agent Team 工具 + run_server 默认接线。

覆盖：
  1. register_team_tools：8 个协作工具入册、按离线节点夹具的依赖映射自动组队、认领/完成、
     常驻子 Agent 跑完 WORK→IDLE（收件箱唤醒）→SHUTDOWN 全生命周期。
  2. build_runtime 默认装配：记忆/Skill/压缩/会话/checkpoint/团队全部默认生效；
     剪辑节点只来自 Storyline MCP，未连通即无剪辑能力（不降级成本地 mock 链）。
  3. startup 钩子：未完成 checkpoint 自动恢复执行。

运行：  python tests/test_team_wiring.py
"""

from __future__ import annotations

import sys as _sys; from pathlib import Path as _Path; _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

import asyncio
import json
import sys
import tempfile
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from agent_framework.checkpoint import Checkpoint, STATUS_COMPLETED, STATUS_RUNNING
from agent_framework.compress import ContextCompressor
from agent_framework.consumer import CHAT_TOPIC
from agent_framework.editing_agent import node_required_map
from agent_framework.llm import ScriptedLLM
from agent_framework.storage import INTERNAL_IDENTITIES, build_storage
from agent_framework.video_editing import build_node_registry
from agent_framework.task_manager import CLAIMED, COMPLETED, PENDING
from agent_framework.tool import ToolRegistry
from agent_framework.team_tools import register_team_tools

from run_server import build_runtime

_checks = 0
_fails = 0


def check(cond: bool, label: str) -> None:
    global _checks, _fails
    _checks += 1
    print(f"  {'✓' if cond else '✗'} {label}")
    if not cond:
        _fails += 1


async def wait_done(task: asyncio.Task, timeout: float = 5.0):
    return await asyncio.wait_for(asyncio.shield(task), timeout=timeout)


async def part1_team_tools(tmp: Path) -> None:
    print("\n[1] Agent Team 工具与常驻子 Agent")
    llm = ScriptedLLM([("answer", "第一轮完成"), ("answer", "第二轮完成")])
    reg = ToolRegistry()
    st1 = build_storage("memory")
    # DAG 权威在剪辑服务端；离线时依赖映射取自本地节点夹具（与生产同一份拓扑定义）。
    # 子 Agent 的 IDLE 窗口显式收紧：生产默认是「常驻等几十秒再接活」（避免每任务重建），
    # 而本用例要的是在秒级内走完 WORK→IDLE→SHUTDOWN 全生命周期，所以给它一个短窗口。
    # 这不是放松断言——下面照样校验它确实收到了收件箱消息并完成两轮，最后落到 shutdown。
    team = register_team_tools(reg, llm=llm, storage=st1,
                               node_deps=lambda: node_required_map(build_node_registry()),
                               idle_window_sec=0.2, idle_poll_sec=0.01)

    need = {
        "send_message", "read_inbox", "plan_editing_team", "list_tasks",
        "claim_task", "complete_task", "start_subagent", "team_status",
    }
    check(need.issubset(set(reg.tool_names)), f"团队 8 工具全部注册：{sorted(need)}")

    plan = json.loads(await reg.execute("plan_editing_team", {}))
    check(len(plan["created_tasks"]) == 21, f"DAG 自动组队 → 21 个任务（实际 {len(plan['created_tasks'])}）")
    check(await st1.db.count("tasks") == 21, "21 行任务进了 PG tasks 表（不落本地目录）")
    tasks = json.loads(await reg.execute("list_tasks", {}))
    check(len(tasks) == 21 and all(t["status"] == PENDING for t in tasks), "任务板 21 个 pending")
    check(all(t["scope"] == "default:default" for t in tasks),
          "离线直调落在缺省作用域，任务板按 用户:会话 分块")

    ready_ids = [t["id"] for t in tasks if not t["blockedBy"]]
    first = min(ready_ids)
    claimed = json.loads(await reg.execute("claim_task", {"task_id": first, "owner": "tester"}))
    check(claimed["status"] == CLAIMED and claimed["owner"] == "tester", f"claim_task 认领 #{first}")
    done = json.loads(await reg.execute("complete_task", {"task_id": first, "owner": "tester"}))  # 认领者本人交付
    check(done["status"] == COMPLETED, f"complete_task 完成 #{first}")
    downstream_ready = [t["id"] for t in await team.task_manager.ready()]
    check(first not in downstream_ready, "已完成任务不再出现在可认领列表")
    status = json.loads(await reg.execute("team_status", {}))
    check(len(status["tasks"]) == 21, "team_status 能看到任务板")
    edges = await st1.db.select("task_edges")
    check(len(edges) == sum(len(t["blockedBy"]) for t in tasks),
          f"依赖边一Edge一行落在 task_edges：{len(edges)} 行")

    # 常驻子 Agent：用全新空任务板的 team，唤醒只能来自收件箱 → 轮数确定
    llm2 = ScriptedLLM([("answer", "第一轮完成"), ("answer", "第二轮完成")])
    reg2 = ToolRegistry()
    st2 = build_storage("memory")
    team2 = register_team_tools(reg2, llm=llm2, storage=st2,
                                idle_window_sec=0.2, idle_poll_sec=0.01)
    declined = json.loads(await reg2.execute("plan_editing_team", {}))
    check(declined["created_tasks"] == [] and "error" in declined,
          f"DAG 未接入时如实回绝而非静默空板：{declined.get('error')}")
    await reg2.execute("send_message", {"to": "worker", "content": "醒来继续收尾"})
    started = json.loads(await reg2.execute(
        "start_subagent", {"name": "worker", "prompt": "完成第一件任务", "allow_tools": ["read_inbox"]}
    ))
    check(started["started"] == "worker", "start_subagent 已启动 worker")
    sub_task = team2._tasks["worker"]
    result = await wait_done(sub_task)
    check(result["worked_rounds"] == 2, f"WORK 两轮（收件箱消息唤醒第二轮）：{result['worked_rounds']}")
    check(result["replies"] == ["第一轮完成", "第二轮完成"], "两轮答复与脚本一致")
    check(await team2.subagent_manager.get_status("worker") == "shutdown", "无新消息后走 SHUTDOWN 释放")
    srow = (await st2.db.select("subagents", where={"name": "worker"}, limit=1))[0]
    check(srow["prompt"] == "完成第一件任务" and srow["status"] == "shutdown",
          f"子 Agent 档案在 PG subagents 表：{ {k: srow[k] for k in ('scope','status')} }")
    status2 = json.loads(await reg2.execute("team_status", {}))
    check(any(a["name"] == "worker" for a in status2["agents"]), "team_status 可见 worker 档案")
    # 团队会话历史同样只在 PG：子 Agent 的两轮 QA 写进 messages 表
    sub_hist = await st2.messages.history("team", "worker")
    check([r["role"] for r in sub_hist] == ["user", "assistant"] * 2,
          f"子 Agent 会话历史入 messages 表：{[r['role'] for r in sub_hist]}")
    check(not (tmp / "rt1").exists() and not (tmp / "rt1c").exists(),
          "团队不再创建任何本地 runtime_dir 目录")
    await team.shutdown()
    await team2.shutdown()


async def part2_defaults(tmp: Path) -> None:
    print("\n[2] build_runtime 默认装配（MCP/Storyline 显式关闭）")
    llm = ScriptedLLM([("answer", "恢复成功")])
    rt = build_runtime(
        llm=llm,
        team=tmp / "rt2",
        mcp_config=False,
        storyline_config=False,
    )
    names = set(rt.registry.tool_names)
    check({"update_memory", "read_memory"}.issubset(names), "长期记忆工具默认开启")
    check("load_skill" in names, "Skill 系统默认开启（examples/skills 存在即加载）")
    check({"plan_editing_team", "start_subagent", "send_message"}.issubset(names), "Agent Team 默认开启")
    check({"create_cron_job", "list_cron_jobs"}.issubset(names), "Cron 工具在位")
    check("render_video" not in names,
          "Storyline 关闭时无剪辑节点工具：剪辑链路唯一来源是 Storyline MCP")

    # 执行轮也要能解释「本轮为什么没有这个工具」（与规划轮那条对称）。
    # 真机事故：执行轮里模型调了 submit_plan，只拿到 tool 'submit_plan' not found，
    # 看不出这是按设计不提供，于是弹窗问用户「本会话没有 submit_plan 工具，你希望怎么处理？」。
    hint = rt.registry.unknown_tool_hint or ""
    check("submit_plan" in hint and "规划轮" in hint and "ask_user" in hint,
          f"执行轮注册表带说明（规划轮专属工具 + 不许为此问用户）：{hint[:36]}…")
    err = str(await rt.registry.execute("submit_plan", {"plans": []}))
    check("本轮不提供" in err and "not found" not in err,
          f"调规划轮专属工具回原因与正路，不回裸 not found：{err[:60]}")

    # 回归（B4-10）：定时任务与心跳同表、同一个调度循环，到点各自分流。
    # 曾经 run_server 从不启动 cron 循环，而心跳自建的调度器会把到期的用户任务领走
    # 只数拍子 —— 「定时任务在真实服务里从不触发」。
    check(rt.cron is rt.heartbeat.scheduler, "定时任务与心跳共用同一个调度器（不重造）")
    delivered: list[dict] = []
    rt.mq.subscribe(CHAT_TOPIC, "test-cron-routing", delivered.append)
    await rt.mq.start()
    await rt.heartbeat.start()
    hb = await rt.storage.jobs.get_by_name("heartbeat")
    check(hb is not None and hb["schedule"]["kind"] == "every",
          f"心跳是 scheduled_jobs 里 name='heartbeat' 的 every 行：{hb and hb['schedule']}")
    user_job = await rt.cron.create_job(task="到点提醒剪片", name="cron-wiring",
                                        kind="at", run_at=1, delete_after_run=False)
    for row in (hb, user_job.to_row()):          # 两行都推到「已到期」，手动跑一轮调度
        await rt.storage.jobs.set_state(
            row["id"], {"next_run_at_ms": 1, "last_run_at_ms": None}, enabled=True)
    fired = await rt.cron.run_due_now()
    await rt.mq.drain(timeout=2.0)
    check(fired == 2, f"一轮调度领到心跳 + 用户任务两行（实际 {fired}）")
    check(rt.heartbeat.beats == 1, "心跳行到点只数拍子")
    check([p["conversation_id"] for p in delivered] == [user_job.id]
          and delivered[0]["user_id"] == "cron",
          f"只有用户定时任务当对话投进 MQ：{[(p['user_id'], p['conversation_id']) for p in delivered]}")
    check((await rt.storage.jobs.get_by_name("heartbeat"))["state"]["next_run_at_ms"] > 1,
          "心跳跑完已推进到下一轮")
    await rt.cron.delete_job(user_job.id)
    await rt.mq.stop()
    cb = rt.agent.runner.context_builder
    check(isinstance(cb.compressor, ContextCompressor), "上下文分级压缩默认挂载")
    check(cb.compressor.max_tokens == 8000, f"默认压缩阈值 8000（实际 {cb.compressor.max_tokens}）")
    check(rt.checkpoint is not None, "checkpoint 默认开启")
    # 回归：SessionManager 空实例是 falsy，曾因 `or` 判断被丢弃 → 会话不入库
    check(rt.agent.session_manager.storage is rt.storage, "会话历史句柄真正接入 Agent（读装配进来的存储）")
    await rt.agent.handle("smoke_u", "smoke_c", "冒烟一下")
    rows = await rt.storage.messages.history("smoke_u", "smoke_c")
    check([r["role"] for r in rows] == ["user", "assistant"]
          and rows[0]["content"] == "冒烟一下",
          f"handle 后 QA 进 messages 表：{[r['role'] for r in rows]}")
    cps = await rt.storage.db.select("checkpoints")
    entries = sorted(await rt.storage.db.select("checkpoint_entries"), key=lambda e: e["seq"])
    check(len(cps) == 1 and cps[0]["status"] == "completed"
          and cps[0]["session_id"] == "smoke_u:smoke_c" and "messages" not in cps[0],
          "checkpoint 指针行落 checkpoints 表并收尾 completed："
          f"{[(c['session_id'], c['status'], c['head_seq']) for c in cps]}")
    check([e["seq"] for e in entries] == list(range(len(entries)))
          and len(entries) == cps[0]["head_seq"] + 1
          and {e["kind"] for e in entries} <= {"delta", "full"},
          f"整条 messages 拆成 checkpoint_entries 增量链：{[(e['seq'], e['kind']) for e in entries]}")
    rebuilt = await rt.checkpoint.load(cps[0]["run_id"])
    check(rebuilt is not None
          and any("冒烟一下" in str(m.get("content", "")) for m in rebuilt.messages),
          "指针行 + 增量链能重建出完整 messages（不是每轮整行重写）")
    check(not (tmp / "rt2" / "checkpoints").exists(), "本地 checkpoints 目录已退役")

    rt_off = build_runtime(llm=llm, team=tmp / "rt3", mcp_config=False, storyline_config=False,
                           max_context_tokens=0)
    check(rt_off.agent.runner.context_builder.compressor is None, "max_context_tokens<=0 关闭压缩")
    rt_no = build_runtime(llm=llm, team=False, mcp_config=False, storyline_config=False,
                          use_memory=False, skills_dir=False, use_checkpoint=False)
    check("update_memory" not in rt_no.registry.tool_names and "plan_editing_team" not in rt_no.registry.tool_names,
          "--no-memory/--no-team 等开关可整体禁用")
    check(rt_no.checkpoint is None and rt_no.agent.runner.checkpoint is None,
          "--no-checkpoint 时整条链路无快照管理器")


async def part3_startup_resume(tmp: Path) -> None:
    print("\n[3] startup 钩子：checkpoint 自动恢复")
    llm = ScriptedLLM([("answer", "已恢复该执行")])
    # 指向必然拒连的端口：本机 8001 上可能真有 Storyline 在跑，不能依赖环境
    down_cfg = tmp / "storyline_down.toml"
    down_cfg.write_text('[local_mcp_server]\nhost = "127.0.0.1"\nport = 1\npath = "/mcp"\ntimeout = 2\n',
                        encoding="utf-8")
    rt = build_runtime(llm=llm, team=tmp / "rt5", mcp_config=False, storyline_config=down_cfg)
    # 预置一个未完成 checkpoint（相当于上次进程崩溃现场）
    cp = Checkpoint(
        run_id="resume-1",
        session_id="u_r:c_r",
        message="触发崩溃的那条输入",
        iteration=1,
        messages=[
            {"role": "system", "content": "s"},
            {"role": "user", "content": "触发崩溃的那条输入"},
        ],
        status=STATUS_RUNNING,
    )
    await rt.checkpoint.save(cp)

    await rt.startup()  # storyline 不可达 → 只告警，不再降级成本地 mock 剪辑链
    check("render_video" not in rt.registry.tool_names,
          "Storyline 未连通即无剪辑能力（本地 mock 剪辑链路已摘除，不留第二套图）")
    check(not any(n.startswith("storyline_") for n in rt.registry.tool_names), "未误注册 storyline_* 工具")
    users = {r["id"] for r in await rt.storage.db.select("users")}
    check(set(INTERNAL_IDENTITIES) <= users,
          f"startup 登记内部身份（写入路径已不自建用户行）：{sorted(users)}")

    loaded = None
    for _ in range(100):
        loaded = await rt.checkpoint.load("resume-1")
        if loaded is not None and loaded.status == STATUS_COMPLETED:
            break
        await asyncio.sleep(0.05)
    check(loaded is not None and loaded.status == STATUS_COMPLETED,
          "startup 自动恢复把未完成 run 跑到 completed")
    sess = rt.agent.session_manager.get("u_r:c_r")
    check(sess is not None and any("已恢复该执行" in str(m.get("content", "")) for m in sess.messages),
          "恢复结果写回原会话 u_r:c_r")
    rows = await rt.storage.messages.history("u_r", "c_r")
    check([r["role"] for r in rows] == ["assistant"] and "已恢复该执行" in rows[0]["content"],
          f"恢复轮的答复进了 messages 表而非只留内存（裸会话不再被 owner 校验吞掉）："
          f"{[(r['role'], r['content'][:12]) for r in rows]}")
    await rt.shutdown()


async def main() -> None:
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        await part1_team_tools(tmp)
        await part2_defaults(tmp)
        await part3_startup_resume(tmp)
    print(f"\n{_checks - _fails}/{_checks} 通过" + ("" if _fails == 0 else f"，{_fails} 失败"))
    sys.exit(1 if _fails else 0)


if __name__ == "__main__":
    asyncio.run(main())
