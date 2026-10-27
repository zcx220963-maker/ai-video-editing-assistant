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
from agent_framework.subagent import SubAgentRunner, make_spawn_tool
from agent_framework.tool import Tool
from agent_framework.video_editing import build_node_registry
from agent_framework.task_manager import CLAIMED, COMPLETED, PENDING
from agent_framework.tool import ToolRegistry
from agent_framework.team_tools import register_team_tools

from run_server import build_runtime, DEFAULT_MAX_CONTEXT_TOKENS

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
    team1_root = tmp / "team1"
    team = register_team_tools(reg, llm=llm, storage=st1,
                               node_deps=lambda: node_required_map(build_node_registry()),
                               team_root=str(team1_root),
                               idle_window_sec=0.2, idle_poll_sec=0.01)

    need = {
        "send_message", "read_inbox", "plan_editing_team", "list_tasks",
        "claim_task", "complete_task", "start_subagent", "team_status",
    }
    check(need.issubset(set(reg.tool_names)), f"团队 8 工具全部注册：{sorted(need)}")

    plan = json.loads(await reg.execute("plan_editing_team", {}))
    # 26 = 剪辑链 21 + 零素材那条链的三个终点（plan_motion / render_motion_code /
    # patch_motion_code）
    # + 口播那条链的选区改终点（patch_video）+ 简洁线起点（simple_film，R18）；
    # 节点普查在 test_video_editing 里钉死，这里跟着涨。写一处，四处断言都读它。
    N_TASKS = 26
    check(len(plan["created_tasks"]) == N_TASKS,
          f"DAG 自动组队 → {N_TASKS} 个任务（实际 {len(plan['created_tasks'])}）")
    board_files = list((team1_root / "tasks" / "default" / "default").glob("task_*.json"))
    check(len(board_files) == N_TASKS and await st1.db.count("tasks") == 0,
          f"{N_TASKS} 个任务落文件真相，PG 不留拷贝")
    tasks = json.loads(await reg.execute("list_tasks", {}))
    check(len(tasks) == N_TASKS and all(t["status"] == PENDING for t in tasks),
          f"任务板 {N_TASKS} 个 pending")
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
    check(len(status["tasks"]) == N_TASKS, "team_status 能看到任务板")
    edges = json.loads((team1_root / "tasks" / "default" / "default" / "edges.json")
                       .read_text(encoding="utf-8"))
    check(len(edges) == sum(len(t["blockedBy"]) for t in tasks),
          f"依赖边落 edges.json：{len(edges)} 行")

    # 常驻子 Agent：用全新空任务板的 team，唤醒只能来自收件箱 → 轮数确定
    llm2 = ScriptedLLM([("answer", "第一轮完成"), ("answer", "第二轮完成")])
    reg2 = ToolRegistry()
    st2 = build_storage("memory")
    team2_root = tmp / "team2"
    team2 = register_team_tools(reg2, llm=llm2, storage=st2,
                                team_root=str(team2_root),
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
    srow = json.loads((team2_root / "agents" / "default" / "default" / "worker.json")
                      .read_text(encoding="utf-8"))
    check(srow["prompt"] == "完成第一件任务" and srow["status"] == "shutdown"
          and await st2.db.count("subagents") == 0,
          f"子 Agent 档案落 agents/{{scope}}/{{name}}.json：{ {k: srow[k] for k in ('scope','status')} }")
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
    # T15c/Q29：以代码实际值 `run_server.DEFAULT_MAX_CONTEXT_TOKENS`（2^16）为准——
    # 旧断言写死的 8000 是**早期占位**，不是判据；本任务明确不许「为了让测试绿而把
    # 阈值回退到 8000」。所以这里改成**引用常量**而非再抄一个数：将来调档只改一处，
    # 而「装配时用的到底是不是那个常量」这件事仍然钉得住。
    check(cb.compressor.max_tokens == DEFAULT_MAX_CONTEXT_TOKENS
          and cb.compressor.max_tokens == 65_536,
          f"默认压缩阈值 = DEFAULT_MAX_CONTEXT_TOKENS=65536（实际 {cb.compressor.max_tokens}）")
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


class _StubTool(Tool):
    """只用来占位的空工具：证明「子注册表里有东西」而不是「子注册表是空的」。"""

    def __init__(self, name: str) -> None:
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return "占位"

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs) -> str:  # pragma: no cover - 不会被调用
        return ""


async def part4_spawn_no_recursion() -> None:
    """[4] T49-①：spawn 防递归从注释变成判据，且描述与行为一致（Q65）。

    红法（改前）：子注册表里其实没有 spawn_subagent，但**没有任何断言在读这件事**——
    守它的是 ``subagent.py`` 里一句注释。谁哪天顺手把 ``_registrars`` 换成「把主注册表
    整个传下去」，注释不会红、工具描述也不会红，而模型会开始无限派生。
    """
    print("\n[4] spawn 防递归（T49-①/Q65）")
    outer = ToolRegistry()

    def _registrar(reg: ToolRegistry) -> None:
        # 子 Agent 能用的工具里**故意**塞一个与 spawn 同名的东西之外什么都没放：
        # 判据要证明的是「spawn 不由子注册表提供」，不是「子注册表是空的」。
        reg.register(_StubTool("read_memory"))

    # 这一节从头到尾不发模型调用（只查注册表与描述），所以给个空脚本的 stub 就够。
    idle_llm = ScriptedLLM([])
    runner = SubAgentRunner(idle_llm, [_registrar])
    spawn = make_spawn_tool(idle_llm, [_registrar])
    outer.register(spawn)
    child = runner._build_child_registry()
    check(child.has("read_memory"), "子注册表拿到装配器给的工具（它不是空表）")
    check(not child.has("spawn_subagent"),
          "子注册表 has('spawn_subagent') is False —— 防递归是结构保证，不是注释")
    check(outer.has("spawn_subagent") and not child.has("spawn_subagent"),
          "同一时刻：主注册表有 spawn、子注册表没有（两边真的分开了）")
    desc = spawn.description
    check("默认异步" in desc and "task_id" in desc,
          "工具描述如实写「默认异步、立即回 task_id」（Q65：描述与行为一致）")
    check("wait=true" in desc and "同步返回" in desc,
          "描述给出同步等待的入口（wait=true），与行为一致")


async def part5_spawn_child_toolset(tmp: Path) -> None:
    """[5] 子 Agent 的**真实**工具面：文件 + 联网，且描述承诺的都能做到。

    红法（改前）：`run_server` 给 `make_spawn_tool` 的装配器清单里只写了
    `register_web_tools`，于是子 Agent 手里只有 fetch_url / web_search——**而
    `SpawnTool` 的描述当时写着「子 Agent 可能写文件或联网」**。真机 run 9272c7f59501
    踩的就是这一条：模型派子 Agent 去「把 shots/s01~s20 读出来汇总成 JSON 写进
    shots/spec.json」——那正是子 Agent 该干的活——子 Agent 试了 `file://` 与
    `localhost` 都被拒，如实回报「硬性工具缺失」，模型只好自己重读 20 个文件。

    判据读的是 **`build_runtime` 真装出来的那个子注册表**，不是 run_server 的源码文本、
    更不是描述：改了装配这一跳它才红。最后一条把「描述与行为一致」（Q65）变成判据——
    描述里承诺「能写文件」而子注册表里没有 write_file，就是描述在撒谎。
    """
    print("\n[5] 子 Agent 的工具面（文件 + 联网，读真装配）")
    llm = ScriptedLLM([])
    rt = build_runtime(llm=llm, team=tmp / "rt5", mcp_config=False, storyline_config=False)
    spawn = rt.registry.get("spawn_subagent")
    check(spawn is not None, "主注册表里有 spawn_subagent")
    if spawn is None:
        return
    child = spawn._runner._build_child_registry()
    names = set(child.tool_names)
    check({"read_file", "write_file", "edit_file", "grep"} <= names,
          f"子 Agent 真拿得到文件工具（实得 {sorted(names)}）")
    check({"fetch_url", "web_search"} <= names,
          f"子 Agent 真拿得到联网工具（实得 {sorted(names)}）")
    check(not child.has("spawn_subagent") and not child.has("start_subagent"),
          f"扩容之后子 Agent 仍不能派生（防递归不受这次改动影响）：{sorted(names)}")
    desc = spawn.description
    check("写文件" not in desc or "write_file" in names,
          "描述里承诺的「子 Agent 可能写文件」在装配里是真的（Q65：描述与行为一致）")


async def main() -> None:
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        await part1_team_tools(tmp)
        await part2_defaults(tmp)
        await part3_startup_resume(tmp)
        await part4_spawn_no_recursion()
        await part5_spawn_child_toolset(tmp)
    print(f"\n{_checks - _fails}/{_checks} 通过" + ("" if _fails == 0 else f"，{_fails} 失败"))
    sys.exit(1 if _fails else 0)


if __name__ == "__main__":
    asyncio.run(main())
